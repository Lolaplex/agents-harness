"""Persona manifests: system prompt fragments without tool explosion."""

from __future__ import annotations

import json
from pathlib import Path

PERSONAS_DIR = Path(__file__).parent / "personas"


def list_personas() -> list[dict]:
    rows = []
    if not PERSONAS_DIR.exists():
        return rows
    for path in sorted(PERSONAS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("name", path.stem)
        rows.append(data)
    return rows


def load_persona(name: str) -> dict | None:
    needle = (name or "").strip().lower()
    if not needle:
        return None
    for row in list_personas():
        if str(row.get("name", "")).lower() == needle:
            return row
    return None


def persona_system_append(name: str, *, base: str = "") -> str:
    row = load_persona(name)
    if row is None:
        return base
    parts = [p for p in (base, str(row.get("system_append") or "").strip()) if p]
    return "\n\n".join(parts)
