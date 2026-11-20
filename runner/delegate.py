"""Delegate tasks to isolated subagent turns.

``python -m runner.delegate spawn --task "…"`` starts a detached
``runner.loop --detached-session`` turn and returns ``dlg_<id>``. The subagent
inherits the workspace environment but its own session, so it can run tools
while the parent turn continues. State lives under
``AGENTS_DELEGATES_DIR`` (default ``<workspace>/delegates``):

    delegates/<id>/meta.json   id, task, session, supervisor pid, provider, model
    delegates/<id>/out.log     turn stdout
    delegates/<id>/err.log     turn stderr
    delegates/<id>/exit.json   {"code": N, "finished": ts} written by the supervisor

The supervisor is ``python -m runner.delegate _run <id>``: a tiny child that
runs the loop as a normal subprocess, so the exit code is exact on every
platform (no shell wrappers).

Guards: depth is counted in a host-side ledger at ``<ws>/run/delegate.depth``
written before each spawn (``AGENTS_DELEGATE_DEPTH`` env is kept as a
compatibility hint; effective depth is the max of both). A delegate cannot
spawn past ``AGENTS_DELEGATE_MAX_DEPTH`` (default 1). The ledger is advisory —
same-UID children share the fs — until real container isolation lands.
``--workdir`` must resolve inside the roots the child's workdir lock honors.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _root() -> Path:
    raw = os.environ.get("AGENTS_DELEGATES_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    from agents_workspace import Workspace

    return Workspace.from_env().dir / "delegates"


def _dir(dlg_id: str) -> Path:
    safe = "".join(ch for ch in dlg_id if ch.isalnum() or ch in ("-", "_"))
    if not safe or safe != dlg_id:
        raise DelegateError("invalid delegate id", code=2)
    return _root() / safe


class DelegateError(Exception):
    def __init__(self, message: str, *, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _tail(path: Path, max_lines: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:] if max_lines > 0 else lines)


def _state(ddir: Path) -> dict[str, Any]:
    meta = _read_json(ddir / "meta.json")
    exit_data = _read_json(ddir / "exit.json")
    pid = int(meta.get("pid") or 0)
    running = "code" not in exit_data and _pid_alive(pid)
    return {
        "id": ddir.name,
        "task": meta.get("task") or "",
        "session": meta.get("session") or "",
        "pid": pid,
        "created": meta.get("created") or "",
        "state": "running" if running else "exited",
        "exit_code": exit_data.get("code"),
        "finished": exit_data.get("finished") or "",
    }


def _ledger_path() -> Path:
    # <ws>/run/delegate.depth for the default <ws>/delegates root; scoped inside
    # the delegates dir when AGENTS_DELEGATES_DIR overrides (tests, overlays).
    base = _root() if os.environ.get("AGENTS_DELEGATES_DIR", "").strip() else _root().parent
    return base / "run" / "delegate.depth"


def _ledger_depth() -> int:
    """Depth claimed on disk. Same-UID children could forge the env counter;
    the ledger survives env tampering (advisory until real container isolation)."""
    try:
        return int(_ledger_path().read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _write_ledger(depth: int) -> None:
    path = _ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{depth}\n", encoding="utf-8")
    except OSError:
        pass  # advisory ledger; spawn continues with the env counter


def _depth() -> int:
    try:
        env_depth = int(os.environ.get("AGENTS_DELEGATE_DEPTH", "0") or 0)
    except ValueError:
        env_depth = 0
    return max(env_depth, _ledger_depth())


def _max_depth() -> int:
    try:
        return int(os.environ.get("AGENTS_DELEGATE_MAX_DEPTH", "1") or 1)
    except ValueError:
        return 1


def _checked_workdir(raw: str) -> str:
    """A delegate workdir must stay inside roots the child's lock would allow.

    The child runs with ``AGENTS_WORKDIR_LOCK=1``; letting it point anywhere
    would make the jail boundary whatever the caller asked for. The accepted
    set is the same one the harness lock honors: the locked workdir root plus
    ``agents_workspace.allowed_roots()`` (workspace tree, host infra, allow list).
    """
    from agents_workspace import Workspace, allowed_roots
    from .executor import _under, workdir_root

    candidate = Path(raw).expanduser().resolve(strict=False)
    roots = allowed_roots(Workspace.from_env())
    locked = workdir_root()
    if locked is not None:
        roots.append(locked)
    if not any(_under(root, candidate) for root in roots):
        raise DelegateError("workdir escapes the workspace boundary", code=2)
    return str(candidate)


def spawn(
    task: str,
    *,
    provider: str = "",
    model: str = "",
    persona: str = "",
    workdir: str = "",
) -> dict[str, Any]:
    task = task.strip()
    if not task:
        raise DelegateError("spawn requires --task", code=2)
    depth = _depth()
    if depth >= _max_depth():
        raise DelegateError(
            f"delegate depth limit reached (AGENTS_DELEGATE_DEPTH={depth})", code=2
        )
    if workdir:
        workdir = _checked_workdir(workdir)
    dlg_id = "dlg_" + uuid.uuid4().hex[:12]
    ddir = _root() / dlg_id
    ddir.mkdir(parents=True, exist_ok=True)
    session = "ses_dlg_" + dlg_id[4:]
    meta = {
        "id": dlg_id,
        "task": task,
        "session": session,
        "provider": provider,
        "model": model,
        "persona": persona,
        "workdir": workdir,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(ddir / "meta.json", meta)
    _write_ledger(depth + 1)

    env = dict(os.environ)
    env["AGENTS_DELEGATE"] = dlg_id
    env["AGENTS_DELEGATE_DEPTH"] = str(depth + 1)
    if workdir:
        env["AGENTS_WORKDIR"] = workdir
        env["AGENTS_WORKDIR_LOCK"] = "1"
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": env,
    }
    if sys.platform == "win32":
        # See agents_terminal.proc: DETACHED_PROCESS leaves invalid std handles.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "runner.delegate", "_run", dlg_id],
            **kwargs,
        )
    except OSError as exc:
        raise DelegateError(str(exc))
    meta["pid"] = proc.pid
    _write_json(ddir / "meta.json", meta)
    return {"id": dlg_id, "session": session, "pid": proc.pid, "state": "running"}


def _run_supervisor(dlg_id: str) -> int:
    """Child process: run the delegate turn, capture output, record the exit."""
    ddir = _dir(dlg_id)
    meta = _read_json(ddir / "meta.json")
    session = str(meta.get("session") or "")
    # ``--opt=value`` form everywhere: a task (or id) starting with "-" must not
    # be parsed as another option by the child loop's argparse.
    argv = [
        sys.executable,
        "-m",
        "runner.loop",
        "--detached-session",
        f"--session={session}",
        "--channel=delegate",
        f"--user={dlg_id}",
        f"--message={meta.get('task') or ''}",
        "--complete",
        "--deliver=buffered",
    ]
    if meta.get("provider"):
        argv.append(f"--provider={meta['provider']}")
    if meta.get("model"):
        argv.append(f"--model={meta['model']}")
    if meta.get("persona"):
        argv.append(f"--persona={meta['persona']}")
    out_f = ddir / "out.log"
    err_f = ddir / "err.log"
    try:
        with open(out_f, "wb") as out, open(err_f, "wb") as err:
            proc = subprocess.run(argv, stdout=out, stderr=err, timeout=None)
        code = int(proc.returncode or 0)
    except Exception as exc:  # supervisor itself failed; record it
        err_f.write_text(f"delegate supervisor failed: {exc}", encoding="utf-8")
        code = 1
    _write_json(
        ddir / "exit.json",
        {"code": code, "finished": datetime.now(timezone.utc).isoformat()},
    )
    return code


def _load(dlg_id: str) -> Path:
    ddir = _dir(dlg_id)
    if not (ddir / "meta.json").is_file():
        raise DelegateError("unknown delegate id", code=2)
    return ddir


def status(dlg_id: str) -> dict[str, Any]:
    return _state(_load(dlg_id))


def list_runs() -> dict[str, Any]:
    root = _root()
    rows = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "meta.json").is_file():
                rows.append(_state(child))
    return {"delegates": rows}


def result(dlg_id: str, *, timeout_sec: float = 600, tail: int = 200) -> dict[str, Any]:
    ddir = _load(dlg_id)
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        state = _state(ddir)
        if state["state"] == "exited":
            state["stdout"] = _tail(ddir / "out.log", tail)
            state["stderr"] = _tail(ddir / "err.log", tail)
            return state
        time.sleep(0.5)
    state = _state(ddir)
    state["timed_out"] = True
    return state


def _kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
        )
        return
    import signal

    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.2)
    if _pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def cancel(dlg_id: str) -> dict[str, Any]:
    ddir = _load(dlg_id)
    state = _state(ddir)
    if state["state"] == "exited":
        return {"id": dlg_id, "state": "exited", "exit_code": state.get("exit_code")}
    _kill_tree(int(state.get("pid") or 0))
    if not (ddir / "exit.json").is_file():
        _write_json(
            ddir / "exit.json",
            {"code": -9, "finished": datetime.now(timezone.utc).isoformat(), "cancelled": True},
        )
    return {"id": dlg_id, "state": "exited", "exit_code": -9}


def _emit(payload: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if "delegates" in payload:
        rows = payload["delegates"]
        if not rows:
            print("(no delegates)")
        for r in rows:
            code = "" if r["exit_code"] is None else f" exit={r['exit_code']}"
            task = (r["task"] or "")[:60]
            print(f"{r['id']}\t{r['state']}{code}\t{task}")
        return 0
    print(f"{payload.get('id', '')} {payload.get('state', '')}", end="")
    if payload.get("exit_code") is not None:
        print(f" exit={payload['exit_code']}", end="")
    print()
    if payload.get("timed_out"):
        print("(wait timed out, still running)")
    for key in ("stdout", "stderr"):
        if payload.get(key):
            print(payload[key])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runner.delegate")
    parser.add_argument("--help-json", action="store_true", help="Emit machine-readable spec.")
    sub = parser.add_subparsers(dest="action")

    sp = sub.add_parser("spawn", help="Start a detached delegate turn")
    sp.add_argument("--task", required=True)
    sp.add_argument("--provider", default="")
    sp.add_argument("--model", default="")
    sp.add_argument("--persona", default="")
    sp.add_argument("--workdir", default="")
    sp.add_argument("--json", action="store_true")

    st = sub.add_parser("status", help="Delegate state")
    st.add_argument("id")
    st.add_argument("--json", action="store_true")

    rs = sub.add_parser("result", help="Wait for the turn and return captured output")
    rs.add_argument("id")
    rs.add_argument("--timeout", type=float, default=600)
    rs.add_argument("--tail", type=int, default=200)
    rs.add_argument("--json", action="store_true")

    ls = sub.add_parser("list", help="List delegates")
    ls.add_argument("--json", action="store_true")

    cc = sub.add_parser("cancel", help="Kill a running delegate tree")
    cc.add_argument("id")
    cc.add_argument("--json", action="store_true")

    rn = sub.add_parser("_run", help=argparse.SUPPRESS)
    rn.add_argument("id")

    args = parser.parse_args(argv)
    if args.help_json:
        print(
            json.dumps(
                {
                    "name": "mcp.delegate",
                    "description": "Spawn and manage isolated subagent turns (delegate_task).",
                    "commands": {
                        "spawn": {
                            "usage": "spawn --task TEXT [--provider NAME] [--model ID] [--persona ID] [--workdir DIR]",
                            "side_effects": "mutating",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "task": {"type": "string", "description": "Task the subagent turn works on (required)"},
                                    "provider": {"type": "string", "description": "Provider name for the turn"},
                                    "model": {"type": "string", "description": "Model id override"},
                                    "persona": {"type": "string", "description": "Persona id"},
                                    "workdir": {"type": "string", "description": "Session working dir; must stay inside the workspace boundary"},
                                },
                            },
                        },
                        "status": {
                            "usage": "status ID",
                            "side_effects": "read-only",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "description": "dlg_<id>"},
                                },
                                "required": ["id"],
                            },
                        },
                        "result": {
                            "usage": "result ID [--timeout SEC] [--tail N]",
                            "side_effects": "read-only",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "description": "dlg_<id>"},
                                    "timeout": {"type": "number", "description": "Max seconds to wait (default 600)"},
                                    "tail": {"type": "integer", "description": "Max output lines (default 200)"},
                                },
                                "required": ["id"],
                            },
                        },
                        "list": {
                            "usage": "list",
                            "side_effects": "read-only",
                        },
                        "cancel": {
                            "usage": "cancel ID",
                            "side_effects": "mutating",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "description": "dlg_<id>"},
                                },
                                "required": ["id"],
                            },
                        },
                    },
                },
                indent=2,
            )
        )
        return 0
    if args.action == "_run":
        return _run_supervisor(args.id)
    try:
        if args.action == "spawn":
            payload = spawn(
                args.task,
                provider=args.provider,
                model=args.model,
                persona=args.persona,
                workdir=args.workdir,
            )
        elif args.action == "status":
            payload = status(args.id)
        elif args.action == "result":
            payload = result(args.id, timeout_sec=args.timeout, tail=args.tail)
        elif args.action == "list":
            payload = list_runs()
        elif args.action == "cancel":
            payload = cancel(args.id)
        else:
            parser.print_help()
            return 0
    except DelegateError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    return _emit(payload, bool(getattr(args, "json", False)))


if __name__ == "__main__":
    raise SystemExit(main())
