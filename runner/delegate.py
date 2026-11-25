"""Delegate tasks to isolated subagent turns.

``python -m runner.delegate spawn --task "…"`` starts a detached
``runner.loop --detached-session`` turn and returns ``dlg_<id>``. The subagent
inherits the workspace environment but its own session, so it can run tools
while the parent turn continues. State lives under
``AGENTS_DELEGATES_DIR`` (default ``<workspace>/run/delegates``, with a
1-release fallback read for legacy ``<workspace>/delegates``):

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

from . import session_gate


def _override_dir() -> Path | None:
    raw = os.environ.get("AGENTS_DELEGATES_DIR", "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _roots() -> tuple[Path, Path | None]:
    """Return ``(primary_delegates_dir, legacy_delegates_dir_or_none)``."""
    override = _override_dir()
    if override is not None:
        return override, None
    from agents_workspace import Workspace

    ws = Workspace.from_env()
    primary = getattr(ws, "delegates", None) or (ws.dir / "run" / "delegates")
    legacy = ws.dir / "delegates"
    return primary, (legacy if legacy != primary else None)


def _root() -> Path:
    return _roots()[0]


def _safe_id(dlg_id: str) -> str:
    safe = "".join(ch for ch in dlg_id if ch.isalnum() or ch in ("-", "_"))
    if not safe or safe != dlg_id:
        raise DelegateError("invalid delegate id", code=2)
    return safe


def _dir(dlg_id: str) -> Path:
    return _root() / _safe_id(dlg_id)


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
    # <ws>/run/delegate.depth for the default ws.delegates root; scoped inside
    # the delegates dir when AGENTS_DELEGATES_DIR overrides (tests, overlays).
    override = _override_dir()
    if override is not None:
        return override / "run" / "delegate.depth"
    from agents_workspace import Workspace

    ws = Workspace.from_env()
    run_dir = getattr(ws, "run", None) or (ws.dir / "run")
    return run_dir / "delegate.depth"


def _recompute_ledger() -> int:
    """Recompute active delegate depth from live runs without exit.json."""
    max_active = 0
    try:
        primary, legacy = _roots()
    except Exception:
        primary, legacy = None, None
    seen: set[str] = set()
    for root in (primary, legacy):
        if root is None or not root.is_dir():
            continue
        for child in root.iterdir():
            if child.name in seen or not child.is_dir():
                continue
            meta_path = child / "meta.json"
            if not meta_path.is_file():
                continue
            seen.add(child.name)
            if (child / "exit.json").is_file():
                continue
            meta = _read_json(meta_path)
            try:
                pid = int(meta.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid <= 0 or not _pid_alive(pid):
                continue
            try:
                run_depth = int(meta.get("depth") or 1)
            except (TypeError, ValueError):
                run_depth = 1
            max_active = max(max_active, max(1, run_depth))
    try:
        path = _ledger_path()
    except Exception:
        return max_active
    if max_active > 0:
        _write_ledger(max_active)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            _write_ledger(0)
    return max_active


def _ledger_depth() -> int:
    """Depth claimed on disk. Same-UID children could forge the env counter;
    the ledger survives env tampering (advisory until real container isolation)."""
    _recompute_ledger()
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
    would make the jail boundary whatever the caller asked for.
    ``agents_workspace.denied_paths(ws)`` is enforced before
    ``allowed_roots(ws)`` and the locked workdir root so sensitive host and
    workspace paths are always refused.
    """
    from agents_workspace import Workspace, allowed_roots, denied_paths
    from .executor import _under, workdir_root

    ws = Workspace.from_env()
    candidate = Path(raw).expanduser().resolve(strict=False)
    if any(_under(denied, candidate) for denied in denied_paths(ws)):
        raise DelegateError("workdir escapes the workspace boundary", code=2)
    roots = allowed_roots(ws)
    locked = workdir_root()
    if locked is not None:
        roots.append(locked)
    if not any(_under(root, candidate) for root in roots):
        raise DelegateError("workdir escapes the workspace boundary", code=2)
    return str(candidate)


def _resolve_workspace():
    """Resolve the active Workspace without creating one for standalone overrides."""
    try:
        from agents_workspace import Host, Workspace
    except ImportError:
        return None
    if _override_dir() is None:
        return Workspace.from_env()
    host = Host.from_env()
    if os.environ.get("AGENTS_WORKSPACE_ROOT", "").strip() or host.has_legacy_layout():
        try:
            return Workspace.from_env()
        except ValueError:
            return None
    env_ws = os.environ.get("AGENTS_WORKSPACE", "").strip()
    if env_ws:
        if not host.workspace_exists(env_ws):
            return None
        return Workspace.from_env()
    if not host.list_workspaces():
        return None
    try:
        return Workspace.from_env()
    except ValueError:
        return None


def _spawn_env(
    dlg_id: str,
    *,
    depth: int,
    max_depth: int,
    persona: str = "",
    workdir: str = "",
) -> dict[str, str]:
    ws = _resolve_workspace()
    if ws is not None:
        env = ws.env(
            os.environ,
            role="turn",
            persona=persona,
            workdir=workdir or None,
        )
    else:
        env = dict(os.environ)
        try:
            from agents_workspace import INFRA_SECRET_KEYS, REMOVED_ENV
        except ImportError:
            INFRA_SECRET_KEYS = (
                "RELAY_SECRET",
                "GATEWAY_SECRET",
                "RELAY_WEB_TOKEN",
                "TELEGRAM_BOT_TOKEN",
                "WEBHOOK_SECRET",
                "DOCKER_HOST",
                "AGENTS_SYNC_SERVER_SECRET",
                "AGENTS_TERMINAL_NOTIFY_SECRET",
            )
            REMOVED_ENV = ("AGENTS_DIR",)
        for key in (*INFRA_SECRET_KEYS, *REMOVED_ENV, "AGENTS_SYNC_TOKEN"):
            env.pop(key, None)
        if persona:
            env["AGENTS_PERSONA"] = persona
    if persona and ws is not None:
        try:
            from .personas import apply_memory_env

            apply_memory_env(persona, env, ws)
        except Exception:
            pass
    if workdir:
        env["AGENTS_WORKDIR"] = workdir
        env["AGENTS_WORKDIR_LOCK"] = "1"
    env["AGENTS_DELEGATE"] = dlg_id
    env["AGENTS_DELEGATE_DEPTH"] = str(depth)
    try:
        from agents_workspace import delegate_env

        env = delegate_env(env, max_depth=max_depth)
    except ImportError:
        env["AGENTS_DELEGATE_DEPTH"] = str(depth + 1)
    except ValueError as exc:
        raise DelegateError(str(exc), code=2) from exc
    return env


def bind_parent_session() -> None:
    """Bind the forwarded parent session before any child env is captured
    or inbox state mutates.

    A spawned local turn is a host-local effect (harness#42), and the
    bind may switch ``AGENTS_WORKDIR`` to the session worktree — a child
    env built from a pre-bind ``os.environ`` would route the delegate
    into the main repo."""
    parent_session = os.environ.get("AGENTS_PARENT_SESSION", "").strip()
    if not parent_session:
        return
    try:
        session_gate.maybe_bind(parent_session, "mcp.delegate")
    except session_gate.SessionGateError as exc:
        raise DelegateError(str(exc), code=2) from exc


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
    _recompute_ledger()
    depth = _depth()
    max_depth = _max_depth()
    if depth >= max_depth:
        raise DelegateError(
            f"delegate depth limit reached (AGENTS_DELEGATE_DEPTH={depth})", code=2
        )
    if workdir:
        workdir = _checked_workdir(workdir)
    dlg_id = "dlg_" + uuid.uuid4().hex[:12]
    session = "ses_dlg_" + dlg_id[4:]
    bind_parent_session()
    env = _spawn_env(
        dlg_id,
        depth=depth,
        max_depth=max_depth,
        persona=persona,
        workdir=workdir,
    )
    ddir = _root() / dlg_id
    ddir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": dlg_id,
        "task": task,
        "session": session,
        "depth": depth + 1,
        "provider": provider,
        "model": model,
        "persona": persona,
        "workdir": workdir,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(ddir / "meta.json", meta)
    _write_ledger(depth + 1)

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
        _write_json(
            ddir / "exit.json",
            {"code": 1, "finished": datetime.now(timezone.utc).isoformat(), "error": str(exc)},
        )
        _recompute_ledger()
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
    code = 1
    try:
        with open(out_f, "wb") as out, open(err_f, "wb") as err:
            proc = subprocess.run(argv, stdout=out, stderr=err, timeout=None)
        code = int(proc.returncode or 0)
    except Exception as exc:  # supervisor itself failed; record it
        err_f.write_text(f"delegate supervisor failed: {exc}", encoding="utf-8")
        code = 1
    finally:
        _write_json(
            ddir / "exit.json",
            {"code": code, "finished": datetime.now(timezone.utc).isoformat()},
        )
        _recompute_ledger()
    return code


def _load(dlg_id: str) -> Path:
    safe = _safe_id(dlg_id)
    primary, legacy = _roots()
    ddir = primary / safe
    if (ddir / "meta.json").is_file():
        return ddir
    if legacy is not None:
        legacy_dir = legacy / safe
        if (legacy_dir / "meta.json").is_file():
            return legacy_dir
    raise DelegateError("unknown delegate id", code=2)


def status(dlg_id: str) -> dict[str, Any]:
    return _state(_load(dlg_id))


def list_runs() -> dict[str, Any]:
    primary, legacy = _roots()
    rows = []
    seen: set[str] = set()
    for root in (primary, legacy):
        if root is None or not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.name in seen:
                continue
            if child.is_dir() and (child / "meta.json").is_file():
                seen.add(child.name)
                rows.append(_state(child))
    rows.sort(key=lambda r: r["id"])
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
        _recompute_ledger()
        return {"id": dlg_id, "state": "exited", "exit_code": state.get("exit_code")}
    _kill_tree(int(state.get("pid") or 0))
    if not (ddir / "exit.json").is_file():
        _write_json(
            ddir / "exit.json",
            {"code": -9, "finished": datetime.now(timezone.utc).isoformat(), "cancelled": True},
        )
    _recompute_ledger()
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
