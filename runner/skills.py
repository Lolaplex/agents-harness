"""On-disk skills: ``<ws>/skills/*/SKILL.md``.

``AGENTS_SKILLS_DIR`` (test/debug override on the CLI-selected workspace)
replaces the default directory. ``AGENTS_SKILLS_EXTRA`` is an
``os.pathsep``-separated list of additional directories; later dirs win
on name. Bodies are returned by ``skill.load``. They are not job-kernel terms.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents_workspace import Workspace


def skill_dirs(workspace: "Workspace | None" = None) -> list[Path]:
    if workspace is not None:
        primary = workspace.skills
    else:
        raw = os.environ.get("AGENTS_SKILLS_DIR", "").strip()
        if raw:
            primary = Path(raw).expanduser()
        else:
            from agents_workspace import Workspace

            primary = Workspace.from_env().skills
    dirs = [primary]
    extra = os.environ.get("AGENTS_SKILLS_EXTRA", "").strip()
    if extra:
        for part in extra.split(os.pathsep):
            part = part.strip()
            if part:
                dirs.append(Path(part).expanduser())
    return dirs


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        val = val.strip().strip('"').strip("'")
        if key:
            meta[key] = val
    return meta, text[end + 4 :]


def _read_skill(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, _body = _parse_frontmatter(text)
    name = meta.get("name") or path.parent.name
    return {
        "name": name,
        "description": meta.get("description") or "",
        "path": str(path),
        "body": text,
    }


def list_skills(workspace: "Workspace | None" = None) -> list[dict[str, Any]]:
    """Name, description, and path. Later directories override earlier names."""
    found: dict[str, dict[str, Any]] = {}
    for folder in skill_dirs(workspace):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*/SKILL.md")):
            row = _read_skill(path)
            if row is None:
                continue
            found[str(row["name"])] = row
    return [found[key] for key in sorted(found)]


def load_skill(name: str, workspace: "Workspace | None" = None) -> str | None:
    """Full SKILL.md text, or None when the name is unknown."""
    needle = (name or "").strip().lower()
    if not needle:
        return None
    for row in list_skills(workspace):
        if str(row["name"]).lower() == needle:
            return str(row["body"])
    return None


def skills_prompt_block(workspace: "Workspace | None" = None) -> str:
    rows = list_skills(workspace)
    if not rows:
        return ""
    lines = ["Skills (call skill.load with the name to read the full SKILL.md):"]
    for row in rows:
        desc = str(row.get("description") or "").strip()
        if desc:
            lines.append(f"- {row['name']}: {desc}")
        else:
            lines.append(f"- {row['name']}")
    return "\n".join(lines)


def format_skill_list(
    rows: list[dict[str, Any]] | None = None,
    workspace: "Workspace | None" = None,
) -> str:
    items = list_skills(workspace) if rows is None else rows
    if not items:
        return "(no skills)"
    lines = []
    for row in items:
        desc = str(row.get("description") or "").replace("\n", " ").strip()
        lines.append(f"{row['name']}\t{desc}".rstrip())
    return "\n".join(lines)
