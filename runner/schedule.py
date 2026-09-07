"""Dynamic schedule & reminder manager for agents-harness.

Pure Python standard library. Stores dynamic schedule manifests in ~/.agents/schedules/
(or AGENTS_SCHEDULES_DIR). Invoked via CLI or as Cordis tool.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone, tzinfo
import json
import os
from pathlib import Path
import re
import sys
import uuid
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .executor import execute_job, load_manifest


def dynamic_schedules_dir() -> Path:
    """Directory holding dynamic schedule manifests."""
    env_dir = os.environ.get("AGENTS_SCHEDULES_DIR", "").strip()
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
    else:
        p = Path.home() / ".agents" / "schedules"
    p.mkdir(parents=True, exist_ok=True)
    return p


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
) -> Dict[str, Any]:
    """Add a dynamic schedule or reminder manifest."""
    target_dir = dynamic_schedules_dir()
    slug = (name or "").strip()
    if not slug:
        slug = f"rem_{int(datetime.now(timezone.utc).timestamp())}_{uuid.uuid4().hex[:6]}"
    slug = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", slug)

    channel = (channel or "").strip()
    user = (user or "").strip()
    timezone_name = (timezone_name or "").strip()

    if not verb:
        if not text:
            raise ValueError("Either 'verb' or 'text' must be provided.")
        if not channel or not user:
            raise ValueError("text without verb requires --channel and --user")
        parts = ["python", "-m", "runner.loop", "--channel", channel, "--user", str(user)]
        clean_text = str(text).replace('"', '\\"')
        parts.extend(["--message", f'"{clean_text}"', "--complete"])
        verb = " ".join(parts)

    manifest: Dict[str, Any] = {
        "name": slug,
        "verb": verb,
        "rests_on": f"Dynamic schedule {slug}",
        "expected_exit": 0,
        "timeout_sec": 60,
    }

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

    if text:
        manifest["text"] = str(text)
    if channel:
        manifest["channel"] = str(channel)
    if user:
        manifest["user"] = str(user)
    if timezone_name:
        manifest["timezone"] = timezone_name

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


def tick(base_time: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Check and execute due schedules. Clean up completed one-shot tasks."""
    now = base_time or datetime.now(timezone.utc)
    schedules = list_dynamic_schedules()
    executed: List[Dict[str, Any]] = []

    for s in schedules:
        is_due = False
        if "at" in s:
            try:
                due_dt = parse_due_time(s["at"])
                if due_dt <= now:
                    is_due = True
            except Exception:
                pass
        elif "cron" in s:
            if is_cron_due(s["cron"], now):
                is_due = True

        if is_due:
            res = execute_job(s)
            executed.append({"schedule": s["name"], "result": res})
            if s.get("one_shot", False):
                remove_schedule(s["name"])

    return executed


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
