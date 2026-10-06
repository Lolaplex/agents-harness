"""Dynamic schedule & reminder manager for agents-harness.

Pure Python standard library. Stores dynamic schedule manifests in ~/.agents/schedules/
(or AGENTS_SCHEDULES_DIR). Invoked via CLI, as a Cordis tool, or in-process
``tick()`` (klanker serve). Cron is evaluated in each job's timezone. A file
lock plus a persisted last-run minute keep overlapping ticks from double-firing,
and a missed minute still runs once inside the grace window (default 5).
The lock is held only while claiming due slots. Jobs then run outside it.
``kind: routine`` jobs are handed to ``register_routine_handler``. A SKIPPED
routine (no handler, or the handler returns SKIPPED) does not consume the slot
or a one-shot manifest.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, tzinfo
import json
import os
from pathlib import Path
import re
import sys
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .executor import execute_job

DEFAULT_GRACE_MIN = 5
DEFAULT_LLM_TIMEOUT = 300
_STATE_NAME = "tick-state.json"
_LOCK_NAME = "tick.lock"
_PROCESS_LOCK = threading.Lock()
_routine_handler: Optional[Callable[[Dict[str, Any]], Any]] = None


def dynamic_schedules_dir() -> Path:
    """Directory holding dynamic schedule manifests."""
    env_dir = os.environ.get("AGENTS_SCHEDULES_DIR", "").strip()
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
    else:
        p = Path.home() / ".agents" / "schedules"
    p.mkdir(parents=True, exist_ok=True)
    return p


def configured_timezone() -> str:
    """Job default zone: AGENTS_TIMEZONE, then TZ, then ~/.agents/config.json, else UTC."""
    named = os.environ.get("AGENTS_TIMEZONE", "").strip()
    if named:
        return named
    tz = os.environ.get("TZ", "").strip()
    if tz:
        return tz
    candidates: list[Path] = []
    home = os.environ.get("AGENTS_HOME", "").strip()
    if home:
        candidates.append(Path(home).expanduser() / "config.json")
    candidates.append(Path.home() / ".agents" / "config.json")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and str(data.get("timezone") or "").strip():
            return str(data["timezone"]).strip()
    return "UTC"


def register_routine_handler(
    handler: Optional[Callable[[Dict[str, Any]], Any]],
) -> Optional[Callable[[Dict[str, Any]], Any]]:
    """Klanker registers this. The harness does not run routine prompts itself."""
    global _routine_handler
    _routine_handler = handler
    return handler


def _is_llm_job(manifest: Dict[str, Any]) -> bool:
    if str(manifest.get("kind") or "") == "routine" or manifest.get("prompt"):
        return True
    verb = str(manifest.get("verb") or "")
    return "runner.loop" in verb and "--complete" in verb


def _job_timeout(manifest: Dict[str, Any]) -> int:
    raw = manifest.get("timeout_sec")
    if raw not in (None, ""):
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return DEFAULT_LLM_TIMEOUT if _is_llm_job(manifest) else 60


def _job_grace(manifest: Dict[str, Any]) -> int:
    raw = manifest.get("grace_min")
    if raw not in (None, ""):
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    env = os.environ.get("AGENTS_SCHEDULE_GRACE_MIN", "").strip()
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    return DEFAULT_GRACE_MIN


def _state_path() -> Path:
    return dynamic_schedules_dir() / _STATE_NAME


def _load_state() -> Dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {"jobs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"jobs": {}}
    if not isinstance(data, dict):
        return {"jobs": {}}
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        data["jobs"] = {}
    return data


def _save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _parse_slot(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@contextmanager
def _file_lock(directory: Path):
    """Exclusive lock so two processes cannot tick the same directory together."""
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / _LOCK_NAME, "a+", encoding="utf-8")
    locked = False
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except ImportError:
            locked = False
        yield
    finally:
        if locked:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _normalize_handler_result(job: Dict[str, Any], value: Any) -> Dict[str, Any]:
    if isinstance(value, dict) and value.get("status"):
        return value
    text = "" if value is None else str(value)
    return {
        "status": "SUCCESS",
        "job": job.get("name"),
        "exit_code": 0,
        "stdout_tail": text[-1000:],
        "stderr_tail": "",
    }


def run_routine(job: Dict[str, Any]) -> Dict[str, Any]:
    """Hand a routine job to the registered handler.

    No handler and a verb: run the verb (previous executor behavior).
    No handler and no verb: skip. The harness does not start a model turn.
    """
    handler = _routine_handler
    prepared = dict(job)
    prepared["timeout_sec"] = _job_timeout(prepared)
    if handler is None:
        if prepared.get("verb"):
            return execute_job(prepared)
        return {
            "status": "SKIPPED",
            "job": prepared.get("name"),
            "exit_code": 0,
            "stdout_tail": "",
            "stderr_tail": "no routine handler registered",
        }

    timeout = _job_timeout(prepared)
    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = handler(prepared)
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=_target, name=f"routine-{prepared.get('name')}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return {
            "status": "TIMEOUT",
            "job": prepared.get("name"),
            "exit_code": -1,
            "stdout_tail": "",
            "stderr_tail": f"routine timeout after {timeout}s",
        }
    if "error" in box:
        return {
            "status": "ERROR",
            "job": prepared.get("name"),
            "exit_code": -1,
            "stdout_tail": "",
            "stderr_tail": str(box["error"]),
        }
    return _normalize_handler_result(prepared, box.get("value"))


def _iana_zone(timezone_name: str) -> tzinfo:
    name = (timezone_name or "").strip()
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError, KeyError):
        return timezone.utc


def parse_due_time(
    val: str,
    base_time: Optional[datetime] = None,
    timezone_name: str = "",
) -> datetime:
    """Parse relative (+10m, +1h, +2d) or ISO-8601 datetime strings to UTC.

    Naive ISO (no offset) uses timezone_name when set, otherwise UTC.
    Explicit Z / offsets stay as written.
    """
    val = val.strip()
    now = base_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    m = re.match(r"^\+?(\d+)\s*([smhd])$", val, re.IGNORECASE)
    if m:
        num = int(m.group(1))
        unit = m.group(2).lower()
        if unit == "s":
            return now + timedelta(seconds=num)
        elif unit == "m":
            return now + timedelta(minutes=num)
        elif unit == "h":
            return now + timedelta(hours=num)
        elif unit == "d":
            return now + timedelta(days=num)

    val_iso = val.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(val_iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_iana_zone(timezone_name))
        return parsed.astimezone(timezone.utc)
    except Exception as exc:
        raise ValueError(
            f"Invalid time format '{val}'. Use ISO-8601 (e.g. 2026-09-05T20:00:00Z) or relative (+10m, +1h, +2d)."
        ) from exc


def match_cron_field(field: str, val: int) -> bool:
    """Match a single cron field against an integer value."""
    field = field.strip()
    if field == "*":
        return True
    if "/" in field:
        parts = field.split("/", 1)
        sub = parts[0]
        step = int(parts[1]) if parts[1].isdigit() else 1
        if sub == "*" or not sub:
            return (val % step) == 0
        if sub.isdigit() and val >= int(sub):
            return ((val - int(sub)) % step) == 0
    if "," in field:
        return any(match_cron_field(f, val) for f in field.split(","))
    if "-" in field:
        parts = field.split("-", 1)
        if parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]) <= val <= int(parts[1])
    if field.isdigit():
        return int(field) == val
    return False


def due_cron_slot(
    cron_expr: str,
    now: datetime,
    *,
    timezone_name: str = "",
    last_slot: Optional[datetime] = None,
    grace_min: int = DEFAULT_GRACE_MIN,
) -> Optional[datetime]:
    """Most recent matching minute in the job zone, inside the grace window, after last_slot."""
    zone = _iana_zone(timezone_name or configured_timezone())
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(zone).replace(second=0, microsecond=0)
    cursor = local
    earliest = local - timedelta(minutes=max(0, int(grace_min)))
    matched: Optional[datetime] = None
    while cursor >= earliest:
        if is_cron_due(cron_expr, cursor):
            matched = cursor
            break
        cursor -= timedelta(minutes=1)
    if matched is None:
        return None
    if last_slot is not None:
        prev = last_slot.astimezone(zone).replace(second=0, microsecond=0)
        if matched <= prev:
            return None
    return matched


def is_cron_due(cron_expr: str, now: datetime) -> bool:
    """Check standard 5-part cron expression (minute hour day-of-month month day-of-week)."""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    if not match_cron_field(minute, now.minute):
        return False
    if not match_cron_field(hour, now.hour):
        return False
    if not match_cron_field(dom, now.day):
        return False
    if not match_cron_field(month, now.month):
        return False
    cron_dow = (now.weekday() + 1) % 7
    if not match_cron_field(dow, cron_dow):
        return False
    return True


def add_schedule(
    *,
    name: Optional[str] = None,
    at: Optional[str] = None,
    cron: Optional[str] = None,
    cadence: Optional[str] = None,
    verb: Optional[str] = None,
    text: Optional[str] = None,
    channel: str = "",
    user: str = "",
    timezone_name: str = "",
    one_shot: Optional[bool] = None,
    prompt: Optional[str] = None,
    session: Optional[str] = None,
    timeout_sec: Optional[int] = None,
    grace_min: Optional[int] = None,
    kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Add a dynamic schedule, reminder, or routine manifest."""
    target_dir = dynamic_schedules_dir()
    slug = (name or "").strip()
    if not slug:
        slug = f"rem_{int(datetime.now(timezone.utc).timestamp())}_{uuid.uuid4().hex[:6]}"
    slug = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", slug)

    channel = (channel or "").strip()
    user = (user or "").strip()
    timezone_name = (timezone_name or "").strip()
    prompt = (prompt or "").strip()
    session = (session or "").strip()
    kind_name = (kind or "").strip()
    is_routine = bool(prompt) or kind_name == "routine"

    if is_routine and not user:
        raise ValueError("routine requires --user")

    if not verb and not is_routine:
        if not text:
            raise ValueError("Either 'verb', 'text', or 'prompt' must be provided.")
        if not channel or not user:
            raise ValueError("text without verb requires --channel and --user")
        parts = ["python", "-m", "runner.loop", "--channel", channel, "--user", str(user)]
        clean_text = str(text).replace('"', '\\"')
        parts.extend(["--message", f'"{clean_text}"', "--complete"])
        verb = " ".join(parts)

    stored_tz = timezone_name or configured_timezone()
    draft: Dict[str, Any] = {"kind": "routine" if is_routine else "", "verb": verb or "", "prompt": prompt}
    if timeout_sec not in (None, ""):
        timeout_val = max(1, int(timeout_sec))
    else:
        timeout_val = _job_timeout(draft)
    grace_val = DEFAULT_GRACE_MIN if grace_min is None else max(0, int(grace_min))

    manifest: Dict[str, Any] = {
        "name": slug,
        "rests_on": f"Dynamic schedule {slug}",
        "expected_exit": 0,
        "timeout_sec": timeout_val,
        "timezone": stored_tz,
        "grace_min": grace_val,
    }
    if verb:
        manifest["verb"] = verb
    if is_routine:
        manifest["kind"] = "routine"
        manifest["prompt"] = prompt
        manifest["session"] = session or f"routine:{slug}"

    if at:
        due_dt = parse_due_time(at, timezone_name=timezone_name)
        manifest["at"] = due_dt.isoformat()
        manifest["cadence"] = "one_shot"
        manifest["one_shot"] = True if one_shot is None else bool(one_shot)
        manifest["rests_on"] = f"One-shot reminder at {manifest['at']} for user {user or 'unknown'}: {text or verb}"
    elif cron:
        manifest["cron"] = cron.strip()
        manifest["cadence"] = cadence or "cron"
        manifest["one_shot"] = bool(one_shot)
        manifest["rests_on"] = f"Recurring cron '{cron}'"
    elif cadence:
        manifest["cadence"] = cadence.strip()
        manifest["one_shot"] = bool(one_shot)
        manifest["rests_on"] = f"Recurring cadence '{cadence}'"
    else:
        raise ValueError("Must specify 'at', 'cron', or 'cadence'.")

    if is_routine and prompt:
        manifest["rests_on"] = f"Routine {slug} for user {user}: {prompt}"
    if text:
        manifest["text"] = str(text)
    if channel:
        manifest["channel"] = str(channel)
    if user:
        manifest["user"] = str(user)

    out_file = target_dir / f"{slug}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return manifest


def list_dynamic_schedules() -> List[Dict[str, Any]]:
    """List all manifests in dynamic schedules dir."""
    target_dir = dynamic_schedules_dir()
    results: List[Dict[str, Any]] = []
    if not target_dir.exists():
        return results
    for p in sorted(target_dir.glob("*.json")):
        if p.name == _STATE_NAME or p.name.startswith("."):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["_file"] = str(p)
            results.append(data)
        except Exception:
            continue
    return results


def remove_schedule(name: str) -> bool:
    """Remove a dynamic schedule by name."""
    target_dir = dynamic_schedules_dir()
    slug = name.strip()
    if slug.endswith(".json"):
        slug = slug[:-5]
    p = target_dir / f"{slug}.json"
    if p.is_file():
        p.unlink()
        return True
    return False


def _job_row(state: Dict[str, Any], name: str) -> Dict[str, Any]:
    jobs = state.setdefault("jobs", {})
    row = jobs.get(name)
    if not isinstance(row, dict):
        row = {}
        jobs[name] = row
    return row


def _inflight(state: Dict[str, Any], name: str, now: datetime) -> bool:
    row = (state.get("jobs") or {}).get(name) or {}
    if not isinstance(row, dict):
        return False
    until = _parse_slot(str(row.get("inflight_until") or ""))
    if until is None:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return until > now


def _set_inflight(state: Dict[str, Any], name: str, now: datetime, timeout_sec: int) -> None:
    row = _job_row(state, name)
    until = now + timedelta(seconds=max(1, int(timeout_sec)) + 30)
    row["inflight_until"] = until.isoformat()


def _mark_slot(state: Dict[str, Any], name: str, slot: datetime) -> None:
    row = _job_row(state, name)
    row["last_slot"] = slot.isoformat()


def _skip_result(schedule: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "SKIPPED",
        "job": schedule.get("name"),
        "exit_code": 0,
        "stdout_tail": "",
        "stderr_tail": "no routine handler registered",
    }


def _will_skip(schedule: Dict[str, Any]) -> bool:
    """Routine with no handler and no verb cannot run. Do not claim its slot."""
    if str(schedule.get("kind") or "") != "routine":
        return False
    if _routine_handler is not None:
        return False
    return not schedule.get("verb")


def _run_due(schedule: Dict[str, Any]) -> Dict[str, Any]:
    prepared = dict(schedule)
    prepared["timeout_sec"] = _job_timeout(prepared)
    if str(prepared.get("kind") or "") == "routine":
        return run_routine(prepared)
    return execute_job(prepared)


def _collect_due(now: datetime) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Claim due work. Caller holds the tick locks. Returns (claimed, skipped)."""
    schedules = list_dynamic_schedules()
    state = _load_state()
    claimed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    dirty = False

    for s in schedules:
        name = str(s["name"])
        if _inflight(state, name, now):
            continue
        if "at" in s:
            try:
                due_dt = parse_due_time(s["at"])
            except Exception:
                continue
            if due_dt > now:
                continue
            if _will_skip(s):
                skipped.append({"schedule": name, "result": _skip_result(s)})
                continue
            _set_inflight(state, name, now, _job_timeout(s))
            dirty = True
            claimed.append({"schedule": s, "one_shot": bool(s.get("one_shot", False))})
        elif "cron" in s:
            job_state = (state.get("jobs") or {}).get(name) or {}
            last = _parse_slot(str(job_state.get("last_slot") or "")) if isinstance(job_state, dict) else None
            slot = due_cron_slot(
                str(s["cron"]),
                now,
                timezone_name=str(s.get("timezone") or ""),
                last_slot=last,
                grace_min=_job_grace(s),
            )
            if slot is None:
                continue
            if _will_skip(s):
                skipped.append({"schedule": name, "result": _skip_result(s)})
                continue
            _mark_slot(state, name, slot)
            dirty = True
            claimed.append({"schedule": s, "one_shot": bool(s.get("one_shot", False))})

    if dirty:
        _save_state(state)
    return claimed, skipped


def _finalize(claimed: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> None:
    """Drop claims for SKIPPED jobs. Consume one-shots that actually ran."""
    by_name = {str(row["schedule"]): row["result"] for row in results}
    state = _load_state()
    dirty = False
    for item in claimed:
        schedule = item["schedule"]
        name = str(schedule["name"])
        result = by_name.get(name) or {}
        status = str(result.get("status") or "")
        row = _job_row(state, name)
        if "inflight_until" in row:
            row.pop("inflight_until", None)
            dirty = True
        if status == "SKIPPED":
            if "last_slot" in row:
                row.pop("last_slot", None)
                dirty = True
            if not row:
                state.get("jobs", {}).pop(name, None)
                dirty = True
            continue
        if item.get("one_shot"):
            remove_schedule(name)
            state.get("jobs", {}).pop(name, None)
            dirty = True
    if dirty:
        _save_state(state)


def _run_claimed(claimed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Run claimed jobs without the tick lock. Parallel so one slow job does not stall the rest."""
    results: List[Optional[Dict[str, Any]]] = [None] * len(claimed)

    def _one(index: int, item: Dict[str, Any]) -> None:
        schedule = item["schedule"]
        results[index] = {"schedule": schedule["name"], "result": _run_due(schedule)}

    threads = [
        threading.Thread(target=_one, args=(i, item), name=f"tick-{item['schedule'].get('name')}")
        for i, item in enumerate(claimed)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [row for row in results if row is not None]


def _tick_body(now: datetime) -> List[Dict[str, Any]]:
    with _PROCESS_LOCK:
        with _file_lock(dynamic_schedules_dir()):
            claimed, skipped = _collect_due(now)
    if not claimed:
        return skipped
    ran = _run_claimed(claimed)
    with _PROCESS_LOCK:
        with _file_lock(dynamic_schedules_dir()):
            _finalize(claimed, ran)
    return skipped + ran


def tick(base_time: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Run due jobs. Safe to call in-process and from overlapping processes.

    The file lock is held only while claiming or rolling back slots, not while
    a job runs. Cron slots are claimed before the job starts so a second tick
    does not double-fire. SKIPPED routines release that claim and keep one-shots.
    """
    now = base_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return _tick_body(now)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="runner.schedule", description="Dynamic schedule & reminder manager")
    sub = parser.add_subparsers(dest="command")

    add_p = sub.add_parser("add", help="Add a dynamic schedule or reminder")
    add_p.add_argument("--name", help="Unique name/slug for the schedule")
    add_p.add_argument("--at", help="Due time: ISO-8601 (2026-09-05T20:00:00Z) or relative (+10m, +1h, +2d)")
    add_p.add_argument("--cron", help="Cron expression (e.g. '0 8 * * *')")
    add_p.add_argument("--cadence", help="Cadence name (e.g. hourly, daily)")
    add_p.add_argument("--verb", help="Direct command to execute")
    add_p.add_argument("--text", help="Reminder text message")
    add_p.add_argument("--channel", default="", help="Target channel (required with --text unless --verb is set)")
    add_p.add_argument("--user", default="", help="Target user/chat ID (required with --text unless --verb is set)")
    add_p.add_argument(
        "--timezone",
        default="",
        dest="timezone_name",
        help="IANA timezone for naive ISO datetimes (e.g. Europe/Berlin)",
    )
    add_p.add_argument("--one-shot", action="store_true", help="Delete manifest after execution")
    add_p.add_argument("--prompt", default="", help="Routine prompt. Handed to the registered handler, not executed here.")
    add_p.add_argument("--session", default="", help="Routine session id (default routine:<name>)")
    add_p.add_argument("--timeout", type=int, default=0, dest="timeout_sec", help="Per-job timeout seconds. LLM jobs and routines default to 300.")
    add_p.add_argument("--grace", type=int, default=-1, dest="grace_min", help="Minutes a missed cron slot may still run once (default 5).")

    sub.add_parser("list", help="List dynamic schedules")

    rem_p = sub.add_parser("remove", help="Remove dynamic schedule")
    rem_p.add_argument("name", help="Schedule name/slug")

    tick_p = sub.add_parser("tick", help="Execute due schedules")
    tick_p.add_argument("--now", help="Simulate current ISO datetime")

    args = parser.parse_args(argv)

    if args.command == "add":
        try:
            res = add_schedule(
                name=args.name,
                at=args.at,
                cron=args.cron,
                cadence=args.cadence,
                verb=args.verb,
                text=args.text,
                channel=args.channel,
                user=args.user,
                timezone_name=getattr(args, "timezone_name", "") or "",
                one_shot=args.one_shot if args.one_shot else None,
                prompt=args.prompt or None,
                session=args.session or None,
                timeout_sec=args.timeout_sec or None,
                grace_min=None if args.grace_min < 0 else args.grace_min,
            )
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0
        except Exception as e:
            print(f"Error adding schedule: {e}", file=sys.stderr)
            return 1

    if args.command == "list":
        rows = list_dynamic_schedules()
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    if args.command == "remove":
        ok = remove_schedule(args.name)
        if ok:
            print(f"Removed schedule '{args.name}'")
            return 0
        else:
            print(f"Schedule '{args.name}' not found", file=sys.stderr)
            return 1

    if args.command == "tick":
        sim_now = parse_due_time(args.now) if args.now else None
        res = tick(sim_now)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
