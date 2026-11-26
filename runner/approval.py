"""Approval gate for mutating model tool calls.

No ``AGENTS_APPROVAL_CMD`` means today's behavior: tools run immediately.
When the command is set, ``AGENTS_APPROVAL_MODE`` selects the gate:

- ``off``: never ask
- ``ask``: mutators only (default when a command is set and the mode is empty).
  Modules with ``approval_ask: false`` (schedule create and list) are not asked.
- ``strict``: every tool that is not read-only, including schedule create

The command gets one JSON object on stdin and exits 0 (approved), 1 (denied),
or 2 (timeout / unavailable, treated as denied). ``{user}`` in the command is
replaced with the turn's channel user before the process starts.

Per-action rules (``runner.approval_policy``) sit on top of these tiers.
``deny`` blocks the call. ``ask`` adds a prompt when a command is set, including
when the tier is ``off``. ``allow`` does not remove a prompt the tier already
requires. With no rules, behavior is unchanged.

The approval command may print ``{"note": "...", "remember": true}`` or
``{"allow_pattern": "<fnmatch>"}`` on stdout: an approved call then persists a
pattern in the allowlist file (``runner.approval_allowlist``) and matching
calls skip the prompt afterwards. ``deny`` rules always still win.

Denied results are harness instructions. They are not untrusted tool data, and
they tell the model not to retry the same call.
"""

from __future__ import annotations

import difflib
import json
import os
import shlex
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .helpjson import READ_ONLY, call_effects, classify_spec


def approval_command() -> str:
    return os.environ.get("AGENTS_APPROVAL_CMD", "").strip()


def _workspace_approval_mode(workspace: Any = None) -> str:
    """``approval.mode`` from the active workspace's ``config.json`` ("" if none)."""
    try:
        from agents_workspace import read_workspace_config

        ws = workspace
        if ws is None:
            from .mcp_client import _resolve_ws_no_create

            ws = _resolve_ws_no_create()
        if ws is None:
            return ""
        cfg = read_workspace_config(ws)
        approval = cfg.get("approval") if isinstance(cfg.get("approval"), dict) else {}
        mode = str(approval.get("mode") or "").strip().lower()
        return mode if mode in ("off", "ask", "strict") else ""
    except Exception:
        return ""


def approval_mode(workspace: Any = None) -> str:
    """Resolved mode. ``AGENTS_APPROVAL_MODE`` > ``<ws>/config.json``
    ``approval.mode`` > empty mode + command => ask; else off."""
    mode = os.environ.get("AGENTS_APPROVAL_MODE", "").strip().lower()
    if mode in ("off", "ask", "strict"):
        return mode
    ws_mode = _workspace_approval_mode(workspace)
    if ws_mode:
        return ws_mode
    if approval_command():
        return "ask"
    return "off"


def module_is_read_only(mod: dict[str, Any]) -> bool:
    """True only when metadata says read-only. Missing metadata is mutating."""
    return classify_spec(mod, default=None) == READ_ONLY


def module_is_mutator(mod: dict[str, Any]) -> bool:
    """Metadata wins. No ``side_effects``, ``mutates``, or read-only hint means mutating.

    The tool name is not evidence. ``AGENTS_APPROVAL_MODE=off`` (the default
    when no approval command is set) still runs the tool without asking.
    """
    return classify_spec(mod) != READ_ONLY


def call_is_read_only(mod: dict[str, Any], args: Any = None) -> bool:
    """Per-command class when arguments name one. Otherwise the module class."""
    if args is not None:
        effects = call_effects(mod, args)
        if effects is not None:
            return effects == READ_ONLY
    return module_is_read_only(mod)


def call_is_mutator(mod: dict[str, Any], args: Any = None) -> bool:
    """Per-command class when arguments name one. Otherwise the module class.

    A declared command with no side-effect metadata is mutating. The command
    name is not consulted. Modules that do not declare commands keep
    ``module_is_mutator``.
    """
    if args is not None:
        effects = call_effects(mod, args)
        if effects is not None:
            return effects != READ_ONLY
    return module_is_mutator(mod)


def _raw_argv_tokens(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return value.split()
    if isinstance(value, (list, tuple)):
        return [str(token).strip() for token in value]
    return []


def _is_verb_option(token: str) -> bool:
    option = token.strip().split("=", 1)[0]
    return len(option) >= 3 and "--verb".startswith(option)


def _verb_arg_rearms(mod: dict[str, Any], args: Any) -> bool:
    """An ``approval_ask: false`` waiver holds only when the call cannot
    queue an arbitrary ``--verb``. A non-empty ``verb`` argument — or a
    ``--verb`` / abbreviation token inside raw argv — re-arms the prompt
    for the mutating class (mcp.schedule.add)."""
    inner = args
    if isinstance(args, dict) and set(args.keys()) == {"arguments"}:
        inner = args.get("arguments")
    argv: Any = None
    if isinstance(inner, dict):
        if str(inner.get("verb") or "").strip():
            return True
        argv = inner.get("argv")
    elif isinstance(inner, (str, list, tuple)):
        argv = inner
    if argv is None:
        return False
    return any(_is_verb_option(token) for token in _raw_argv_tokens(argv))


def needs_approval(
    mod: dict[str, Any], args: Any = None, *, workspace: Any = None
) -> bool:
    mode = approval_mode(workspace)
    if mode == "off" or not approval_command():
        return False
    if args is not None:
        effects = call_effects(mod, args)
        if effects == READ_ONLY:
            return False
        if effects is not None:
            return True
    if mode == "strict":
        return not module_is_read_only(mod)
    if mod.get("approval_ask") is False and not _verb_arg_rearms(mod, args):
        return False
    return module_is_mutator(mod)


_MAX_PREVIEW_BYTES = 64_000
_MAX_FILE_READ_BYTES = 32_768
_MAX_PREVIEW_TEXT_CHARS = 16_000

_HIGH_RISK_BINS = frozenset({
    "rm", "rmdir", "del", "rd", "remove-item", "sudo", "chmod", "chown",
    "curl", "wget", "invoke-webrequest", "iwr", "dd", "mkfs", "format",
    "kill", "taskkill", "stop-process", "reg", "icacls", "net",
})
_HIGH_RISK_SUBSTRS = (
    "git push",
    "git reset --hard",
    "git clean",
    " --force",
    " -rf",
    " /f ",
    " -enc",
)
_LOW_RISK_BINS = frozenset({
    "echo", "pwd", "ls", "dir", "cat", "type", "head", "tail", "wc",
    "pytest", "whoami", "date",
})
_LOW_RISK_GIT = frozenset({"status", "diff", "log", "branch", "show", "rev-parse"})


def _resolve_workdir(workspace: Any = None) -> Path:
    raw = getattr(workspace, "workdir", None) if workspace is not None else None
    if raw:
        return Path(raw).expanduser().resolve(strict=False)
    env_wd = os.environ.get("AGENTS_WORKDIR", "").strip()
    if env_wd:
        return Path(env_wd).expanduser().resolve(strict=False)
    return Path.cwd().resolve(strict=False)


def _confined_path(workdir: Path, raw_path: str) -> tuple[Path | None, str]:
    raw = str(raw_path or "").strip()
    if not raw or "\x00" in raw:
        return None, raw
    from .approval_allowlist import _under

    workdir_r = workdir.expanduser().resolve(strict=False)
    p = Path(raw)
    cand = p if p.is_absolute() else (workdir_r / p)
    try:
        resolved = cand.resolve(strict=False)
    except (OSError, RuntimeError):
        return None, raw
    if resolved == workdir_r or not _under(workdir_r, resolved):
        return None, raw
    try:
        rel = resolved.relative_to(workdir_r).as_posix()
    except ValueError:
        return None, raw
    return resolved, rel


def _classify_command_risk(argv: list[str]) -> str:
    if not argv:
        return "medium"
    head = os.path.basename(str(argv[0]).strip()).lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if head.endswith(ext):
            head = head[: -len(ext)]
            break
    joined = " ".join(str(x) for x in argv).lower()
    if head in _HIGH_RISK_BINS:
        return "high"
    if any(sub in joined for sub in _HIGH_RISK_SUBSTRS):
        return "high"
    if head in ("sh", "bash", "cmd", "powershell", "pwsh") and len(argv) > 1:
        return "high"
    if head in _LOW_RISK_BINS:
        return "low"
    if head == "git" and len(argv) > 1 and str(argv[1]).lower() in _LOW_RISK_GIT:
        return "low"
    return "medium"


def _parse_flag_value(tokens: list[str], index: int, flag: str) -> tuple[str | None, int]:
    tok = tokens[index]
    if tok == flag and index + 1 < len(tokens):
        return tokens[index + 1], index + 2
    if tok.startswith(flag + "="):
        return tok.split("=", 1)[1], index + 1
    return None, index


def _raw_argv_exact(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(token) for token in value]
    return _raw_argv_tokens(value)


def _extract_terminal_op(tool: str, args: Any) -> tuple[str, dict[str, Any]]:
    if not tool.startswith("mcp.terminal"):
        return "", {}
    inner = args
    if isinstance(args, dict) and set(args.keys()) == {"arguments"}:
        inner = args.get("arguments")
    suffix = tool.split(".", 2)[2] if tool.count(".") >= 2 else ""

    if isinstance(inner, dict):
        sub = suffix or str(inner.get("command") or inner.get("verb") or "").strip().lower()
        if sub == "write" or (not sub and "content" in inner and "path" in inner):
            return "write", {
                "path": str(inner.get("path") or ""),
                "content": "" if inner.get("content") is None else str(inner.get("content")),
            }
        if sub == "edit" or (not sub and "old" in inner and "new" in inner and "path" in inner):
            return "edit", {
                "path": str(inner.get("path") or ""),
                "old": "" if inner.get("old") is None else str(inner.get("old")),
                "new": "" if inner.get("new") is None else str(inner.get("new")),
                "replace_all": bool(inner.get("replace_all")),
            }
        raw_argv = inner.get("argv")
        if raw_argv is not None:
            tokens = _raw_argv_exact(raw_argv)
        elif sub in ("run", "bg") and inner.get("cmd") is not None:
            tokens = [sub, *_raw_argv_exact(inner.get("cmd"))]
        else:
            tokens = []
    else:
        tokens = _raw_argv_exact(inner)

    if not tokens:
        return "", {}
    if suffix in ("write", "edit", "run", "bg") and tokens[0].strip() != suffix:
        tokens = [suffix, *tokens]
    op = tokens[0].strip().lower()
    rest = tokens[1:]
    if op == "write":
        path = ""
        content = ""
        i = 0
        pos: list[str] = []
        while i < len(rest):
            val, nxt = _parse_flag_value(rest, i, "--content")
            if val is not None:
                content = val
                i = nxt
                continue
            if rest[i] in ("--stdin", "--create-only", "--json"):
                i += 1
                continue
            pos.append(rest[i])
            i += 1
        if pos:
            path = pos[0]
        if not content and len(pos) > 1:
            content = pos[1]
        return "write", {"path": path, "content": content}
    if op == "edit":
        path = ""
        old = ""
        new = ""
        replace_all = False
        i = 0
        pos = []
        while i < len(rest):
            v_old, nxt = _parse_flag_value(rest, i, "--old")
            if v_old is not None:
                old = v_old
                i = nxt
                continue
            v_new, nxt = _parse_flag_value(rest, i, "--new")
            if v_new is not None:
                new = v_new
                i = nxt
                continue
            if rest[i] == "--replace-all":
                replace_all = True
                i += 1
                continue
            if rest[i] in ("--stdin-json", "--json"):
                i += 1
                continue
            pos.append(rest[i])
            i += 1
        if pos:
            path = pos[0]
        return "edit", {"path": path, "old": old, "new": new, "replace_all": replace_all}
    if op in ("run", "bg"):
        while rest and rest[0] in ("--pty", "--json", "--"):
            flag = rest.pop(0)
            if flag == "--":
                break
        if rest:
            return "command", {"argv": rest}
        return "", {}
    return "command", {"argv": tokens}


def _truncate_text(text: str, limit: int = _MAX_PREVIEW_TEXT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _build_preview(tool: str, args: Any, *, workspace: Any = None) -> dict[str, Any] | None:
    try:
        kind, info = _extract_terminal_op(tool, args)
        if not kind:
            return None
        workdir = _resolve_workdir(workspace)
        if kind == "command":
            argv = [str(x)[:300] for x in (info.get("argv") or [])[:40]]
            if not argv:
                return None
            return {
                "kind": "command",
                "argv": argv,
                "cwd": str(workdir),
                "risk": _classify_command_risk(argv),
            }
        raw_path = str(info.get("path") or "")
        confined, rel = _confined_path(workdir, raw_path)
        if confined is None:
            return {
                "kind": "diff",
                "path": (raw_path or "?")[:200],
                "error": "path escapes workdir",
            }
        old_full = ""
        truncated = False
        if confined.is_file():
            with confined.open("rb") as fh:
                chunk = fh.read(_MAX_FILE_READ_BYTES + 1)
            if len(chunk) > _MAX_FILE_READ_BYTES:
                truncated = True
                chunk = chunk[:_MAX_FILE_READ_BYTES]
            if b"\x00" in chunk:
                return {"kind": "diff", "path": rel, "binary": True}
            old_full = chunk.decode("utf-8", errors="replace")
        if kind == "write":
            new_full = str(info.get("content") or "")
        else:
            old_needle = str(info.get("old") or "")
            new_rep = str(info.get("new") or "")
            if old_needle and old_needle in old_full:
                if info.get("replace_all"):
                    new_full = old_full.replace(old_needle, new_rep)
                else:
                    new_full = old_full.replace(old_needle, new_rep, 1)
            else:
                old_full = old_needle
                new_full = new_rep
        uni_full = "".join(
            difflib.unified_diff(
                old_full.splitlines(True),
                new_full.splitlines(True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=3,
            )
        )
        old_s, t1 = _truncate_text(old_full)
        new_s, t2 = _truncate_text(new_full)
        uni_s, t3 = _truncate_text(uni_full)
        truncated = truncated or t1 or t2 or t3
        preview: dict[str, Any] = {
            "kind": "diff",
            "path": rel,
            "old_text": old_s,
            "new_text": new_s,
            "unified": uni_s,
            "truncated": truncated,
        }
        while len(json.dumps(preview, ensure_ascii=False).encode("utf-8")) > _MAX_PREVIEW_BYTES:
            cap = max(256, len(preview["unified"]) // 2)
            if len(preview["old_text"]) <= 256 and len(preview["new_text"]) <= 256 and len(preview["unified"]) <= 256:
                break
            preview["old_text"] = preview["old_text"][:cap]
            preview["new_text"] = preview["new_text"][:cap]
            preview["unified"] = preview["unified"][:cap]
            preview["truncated"] = True
        return preview
    except Exception:
        return None


def summarize(tool: str, args: Any) -> str:
    if isinstance(args, dict):
        bits: list[str] = []
        for key, val in list(args.items())[:6]:
            text = str(val).replace("\n", " ")
            if len(text) > 80:
                text = text[:77] + "..."
            bits.append(f"{key}={text}")
        detail = ", ".join(bits)
    else:
        detail = str(args).replace("\n", " ")[:160]
    line = f"{tool} {detail}".strip()
    return line[:240]


def _command_argv(user: str) -> list[str]:
    parts = shlex.split(approval_command(), posix=True)
    return [part.replace("{user}", user) for part in parts]


def _timeout_sec(argv: list[str]) -> float:
    env = os.environ.get("AGENTS_APPROVAL_TIMEOUT", "").strip()
    if env:
        try:
            return max(1.0, float(env))
        except ValueError:
            pass
    if "--timeout" in argv:
        idx = argv.index("--timeout")
        if idx + 1 < len(argv):
            try:
                return max(1.0, float(argv[idx + 1]) + 15.0)
            except ValueError:
                pass
    return 330.0


def _stdout_options(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _note_from_stdout(text: str) -> str:
    return str(_stdout_options(text).get("note") or "").strip()


def _jsonable(args: Any) -> Any:
    if isinstance(args, (dict, list, str, int, float, bool)) or args is None:
        return args
    return str(args)


def _denied(req_id: str, tool: str, why: str, note: str = "") -> str:
    msg = f"Denied: {why} (id={req_id}, tool={tool}). Do not retry the same tool call."
    if note:
        msg += f" Note: {note}"
    return msg


def request_approval(
    *,
    tool: str,
    args: Any,
    session: str = "",
    user: str = "",
    summary: str = "",
    actions: list[str] | None = None,
    preview: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Run the approval command. ``(True, "", opts)`` means proceed."""
    req_id = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "id": req_id,
        "session": session or "",
        "user": user or "",
        "tool": tool,
        "args": _jsonable(args),
        "summary": summary or summarize(tool, args),
    }
    if actions:
        payload["actions"] = list(actions)
    if isinstance(preview, dict) and preview:
        payload["preview"] = preview
    argv = _command_argv(user or "")
    if not argv:
        return False, _denied(req_id, tool, "approval command is empty"), {}
    try:
        proc = subprocess.run(
            argv,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=_timeout_sec(argv),
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return False, _denied(req_id, tool, "approval timed out or was unavailable"), {}
    except OSError as exc:
        return False, _denied(
            req_id, tool, "approval timed out or was unavailable", note=str(exc)
        ), {}
    opts = _stdout_options(proc.stdout or "")
    note = str(opts.get("note") or "").strip()
    if proc.returncode == 0:
        return True, "", opts
    if proc.returncode == 2:
        why = "approval timed out or was unavailable"
    else:
        why = "the user declined this tool call"
    return False, _denied(req_id, tool, why, note), opts


def gate_module(
    mod: dict[str, Any],
    args: Any,
    *,
    session: str = "",
    user: str = "",
    workspace: Any = None,
) -> tuple[bool, str]:
    """Return ``(allowed, denial_message)``. Skipped gates are allowed.

    The tier (``needs_approval``) is applied first. A matching ``deny`` rule
    blocks even when the tier would not ask. A matching ``ask`` rule asks
    when an approval command is set. A matching ``allow`` rule does not
    cancel the tier.
    """
    from .approval_policy import PolicyError, action_keys, load_rules, strongest_rule

    tool = str(mod.get("name") or "")
    req_id = str(uuid.uuid4())
    try:
        keys = action_keys(mod, args)
        rule, matched = strongest_rule(load_rules(workspace), keys)
    except PolicyError as exc:
        return False, _denied(req_id, tool, str(exc))
    if rule is not None and rule.decision == "deny":
        return False, _denied(req_id, tool, f"policy denied {matched or rule.action}")
    tier_ask = needs_approval(mod, args, workspace=workspace)
    rule_ask = rule is not None and rule.decision == "ask" and bool(approval_command())
    if not tier_ask and not rule_ask:
        return True, ""
    payload = args if isinstance(args, dict) else {"arguments": args}
    from .approval_allowlist import is_allowed, remember

    if is_allowed(tool, payload, workspace=workspace):
        return True, ""
    summary = summarize(tool, args)
    if keys:
        summary = f"{summary} actions={','.join(keys[:6])}"[:240]
    preview = _build_preview(tool, payload, workspace=workspace)
    allowed, denial, opts = request_approval(
        tool=tool,
        args=payload,
        session=session,
        user=user,
        summary=summary,
        actions=keys,
        preview=preview,
    )
    if allowed and (opts.get("remember") or opts.get("allow_pattern")):
        remember(
            tool,
            payload,
            pattern=str(opts.get("allow_pattern") or ""),
            workspace=workspace,
        )
    return allowed, denial
