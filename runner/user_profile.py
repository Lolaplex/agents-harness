"""Load human profile from ~/.agents/memory/USER.md (not identity.json)."""

from __future__ import annotations

import re
from pathlib import Path

from agents_workspace import Workspace


def _agents_memory_dir() -> Path:
    return Workspace.from_env().memory


def load_user_profile() -> dict[str, str]:
    """Parse ## Who from USER.md. Empty strings when missing."""
    path = _agents_memory_dir() / "USER.md"
    out = {"display": "", "work": "", "timezone": ""}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    who = ""
    if "## Who" in text:
        who = text.split("## Who", 1)[1].split("##", 1)[0]
    for line in who.splitlines():
        m = re.match(r"^\s*-\s*Name:\s*(.+)\s*$", line)
        if m:
            out["display"] = m.group(1).strip()
            continue
        m = re.match(r"^\s*-\s*Work:\s*(.+)\s*$", line)
        if m:
            out["work"] = m.group(1).strip()
            continue
        m = re.match(r"^\s*-\s*Timezone:\s*(.+)\s*$", line, re.I)
        if m:
            out["timezone"] = m.group(1).strip()
    return out
